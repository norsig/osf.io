<%inherit file="base.mako"/>
<%def name="title()">OAuth Application settings</%def>
<%def name="content()">
<% from website import settings %>
<h2 class="page-header">OAuth application settings</h2>

<div class="row">

    <div class="col-sm-3">
        <div class="panel panel-default">
            <ul class="nav nav-stacked nav-pills">
                <li><a href="${ web_url_for('user_profile') }">Profile Information</a></li>
                <li><a href="${ web_url_for('user_account') }">Account Settings</a></li>
                <li><a href="${ web_url_for('user_addons') }">Configure Add-ons</a></li>
                <li><a href="${ web_url_for('user_notifications') }">Notifications</a></li>
                <li><a href="#">Developer apps</a> </li>
            </ul>
        </div><!-- end sidebar -->
    </div>

    <div class="col-sm-9 col-md-7">

        <ul class="nav nav-tabs">
            <!-- TODO "specify homepage" design is suitable for web apps, but not so much CLI utils... one key per user, or per project? -->
            <li class="active"><a href="#registered" data-toggle="tab">API Keys</a></li>
            <li><a href="#create" data-toggle="tab">Create new...</a></li>
        </ul>

        <div class="tab-content">
            <div class="tab-pane active" id="registered">
                <p>You have registered the following applications that can connect to the OSF:</p>

                <table class="table table-condensed">
                    <tr>
                        <th>Application</th>
                        <th>
                            <span class="pull-right">
                                Delete <span class="glyphicon glyphicon-info-sign" aria-hidden="true"
                                             title="Deleting this API key will de-authorize any external applications that use it to connect to the OSF. This cannot be reversed!"></span>
                            </span>
                        </th>
                    </tr>
                    %for reg_app in known_apps:
                        <tr>
                            <td>
                                <a href="${reg_app['home_url']}">${reg_app['name']}</a>
                                <p>Key: <span class="text-muted">${reg_app['id']} </span></p>
                            </td>
                            <td>
                                <button type="button" class="btn btn-danger pull-right">Delete</button>
                            </td>
                        </tr>
                    %endfor
                </table>
            </div> <!-- end registered api keys tab-->

            <div class="tab-pane" id="create">
                <p>If you would like to access the OSF from an external application or website, you will need to register for an API key.
                    See <a href="#">the help that has not yet been written</a> for more details.</p>
                <%include file="include/profile/oauth_app_data.mako" />

            </div> <!-- end create new api key tab -->

            <!-- TODO: Rewrite using KO.js and hide the "you have registered" blurb when registered list is empty -->

        </div>

    </div>
</div>

</%def>
